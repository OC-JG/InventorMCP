"""Reading a drawing into a parametric model, and checking one against the other.

A drawing is a specification, not a picture. Tracing its outlines would give
geometry with no parameters, which is the thing this project exists not to
produce; reading its dimensions gives the driving values. Keeping the reading
separate from the recipe is what lets one be checked against the other, and a
drawing is redundantly specified on purpose, which is what makes the checks
possible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inventor_mcp.builder import rehearse
from inventor_mcp.drawing import DrawingReading, compare
from inventor_mcp.schema import PartRecipe

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

#: A reading of the mounting plate's drawing, as if taken off the sheet.
PLATE_DRAWING = {
    "title": "Mounting Plate", "units": "mm", "projection": "first_angle",
    "scale": "1:1", "material": "Aluminium 6082",
    "views": [
        {"name": "FRONT", "kind": "front", "shows": "width and thickness",
         "extent": [120, 8]},
        {"name": "TOP", "kind": "top", "shows": "the hole pattern",
         "extent": [120, 80]},
    ],
    "dimensions": [
        {"label": "overall width", "value": 120, "view": "TOP"},
        {"label": "overall depth", "value": 80, "view": "TOP"},
        {"label": "thickness", "value": 8, "kind": "thickness", "view": "FRONT"},
        {"label": "hole diameter", "value": 6.6, "kind": "diameter", "count": 4},
        {"label": "hole centre to edge", "value": 12, "view": "TOP"},
        {"label": "corner radius", "value": 10, "kind": "radius"},
    ],
    "notes": ["ALL BURRS REMOVED"],
}


def plate_recipe() -> dict:
    return json.loads((EXAMPLES / "mounting_plate.json").read_text())


def check(reading: dict, recipe: dict) -> dict:
    return compare(DrawingReading.model_validate(reading),
                   rehearse(PartRecipe.model_validate(recipe)))


class TestAReadingIsARecord:
    def test_a_reading_with_nothing_in_it_is_refused(self):
        """An empty reading is not a reading; say what you could not see."""
        with pytest.raises(ValueError, match="not a reading"):
            DrawingReading.model_validate({"units": "mm"})

    def test_saying_what_you_could_not_read_is_enough(self):
        reading = DrawingReading.model_validate(
            {"units": "mm", "unreadable": ["the note block is cut off"]})
        assert reading.dimensions == []

    def test_a_reference_dimension_is_not_expected_to_drive(self):
        reading = DrawingReading.model_validate({"dimensions": [
            {"label": "overall", "value": 90},
            {"label": "(restated overall)", "value": 90, "reference": True},
        ]})
        assert [d.label for d in reading.driving()] == ["overall"]

    def test_an_unknown_field_is_rejected_rather_than_ignored(self):
        with pytest.raises(Exception):
            DrawingReading.model_validate(
                {"dimensions": [{"label": "x", "value": 1, "colour": "red"}]})


class TestTheModelMeetsTheDrawing:
    def test_a_faithful_recipe_matches_every_dimension(self):
        result = check(PLATE_DRAWING, plate_recipe())
        assert result["ok"] is True
        assert len(result["matched"]) == 6
        assert result["missing"] == []

    def test_a_derived_value_is_not_called_an_invention(self):
        """96 from a 120 plate and a 12 margin is what parametric means."""
        result = check(PLATE_DRAWING, plate_recipe())
        assert result["invented"] == []
        derived = {d["value_in_drawing_units"] for d in result.get("derived", [])}
        assert 96.0 in derived

    def test_the_overall_size_is_checked_against_the_views(self):
        result = check(PLATE_DRAWING, plate_recipe())
        assert not any("overall" in m["drawing"] for m in result["missing"])

    def test_a_dimension_the_model_ignores_is_reported(self):
        """The commonest drawing-reading mistake: one dimension overlooked."""
        reading = json.loads(json.dumps(PLATE_DRAWING))
        reading["dimensions"].append(
            {"label": "spigot diameter", "value": 22, "kind": "diameter"})
        result = check(reading, plate_recipe())
        assert result["ok"] is False
        assert any("spigot" in m["drawing"] for m in result["missing"])

    def test_a_misread_dimension_is_reported(self):
        """120 read as 130: the model is self-consistent and wrong."""
        reading = json.loads(json.dumps(PLATE_DRAWING))
        reading["dimensions"][0]["value"] = 130
        reading["views"][0]["extent"] = [130, 8]
        reading["views"][1]["extent"] = [130, 80]
        result = check(reading, plate_recipe())
        assert result["ok"] is False
        assert any("overall width" in m["drawing"] for m in result["missing"])

    def test_a_literal_the_drawing_never_gives_is_reported(self):
        recipe = plate_recipe()
        recipe["operations"].append(
            {"op": "sketch", "name": "Slot", "plane": "xy", "entities": [
                {"type": "circle", "center": [0, 0], "diameter": 34}]})
        recipe["operations"].append(
            {"op": "extrude", "sketch": "Slot", "distance": 3, "operation": "cut"})
        result = check(PLATE_DRAWING, recipe)
        assert any(item["value_in_drawing_units"] == 34.0
                   for item in result["invented"])

    def test_the_part_being_the_wrong_size_is_caught_on_its_own(self):
        """A part the right shape and the wrong size passes everything else."""
        recipe = plate_recipe()
        for spec in recipe["parameters"]:
            if spec["name"] == "thk":
                spec["value"] = 10
        result = check(PLATE_DRAWING, recipe)
        assert result["ok"] is False


class TestThingsWorthWarningAbout:
    def test_an_unstated_projection_is_warned_about(self):
        """First and third angle mirror the part relative to each other."""
        reading = json.loads(json.dumps(PLATE_DRAWING))
        reading["projection"] = "unknown"
        result = check(reading, plate_recipe())
        assert any("projection" in w["warning"] for w in result["warnings"])

    def test_a_stated_projection_is_not_warned_about(self):
        result = check(PLATE_DRAWING, plate_recipe())
        assert not any("projection" in w["warning"] for w in result["warnings"])

    def test_anything_unreadable_is_carried_through_as_a_warning(self):
        reading = json.loads(json.dumps(PLATE_DRAWING))
        reading["unreadable"] = ["the tolerance on the bore"]
        result = check(reading, plate_recipe())
        assert any("tolerance on the bore" in w["warning"]
                   for w in result["warnings"])

    def test_no_overall_size_to_check_against_is_warned_about(self):
        reading = json.loads(json.dumps(PLATE_DRAWING))
        reading["views"] = []
        result = check(reading, plate_recipe())
        assert any("overall size" in w["warning"] for w in result["warnings"])

    def test_two_perpendicular_views_are_enough_to_pin_the_box(self):
        reading = json.loads(json.dumps(PLATE_DRAWING))
        reading.pop("overall", None)
        result = check(reading, plate_recipe())
        assert not any("overall size" in w["warning"] for w in result["warnings"])


class TestThroughTheTool:
    def call(self, server, name, arguments):
        import asyncio

        return asyncio.run(server.call_tool(name, arguments)).structured_content

    def test_the_schema_is_available_to_a_client(self, server):
        payload = self.call(server, "drawing_reading_schema", {})
        schema = payload["json_schema"]
        assert "projection" in schema["properties"]
        assert "unreadable" in schema["properties"]
        assert "DrawingDimension" in schema.get("$defs", {})

    def test_a_faithful_recipe_passes_through_the_tool(self, server):
        payload = self.call(server, "check_against_drawing",
                            {"recipe": plate_recipe(), "reading": PLATE_DRAWING})
        assert payload["ok"] is True
        assert payload["rehearsal"]["result"]["span_mm"] == [120.0, 80.0, 8.0]

    def test_a_recipe_that_does_not_build_says_so_instead(self, server):
        recipe = plate_recipe()
        recipe["operations"][1]["distance"] = "no_such_parameter"
        payload = self.call(server, "check_against_drawing",
                            {"recipe": recipe, "reading": PLATE_DRAWING})
        assert payload["ok"] is False
        assert "does not build" in payload["error"]


class TestAnglesAreNotLengths:
    """A 90 degree angle is 1.5708 in Inventor's units, which is not 15.7 mm."""

    def reading(self, **changes) -> dict:
        base = {
            "units": "mm",
            "projection": "third_angle",
            "overall": [100, 60, 10],
            "dimensions": [
                {"label": "overall length", "value": 100},
                {"label": "overall width", "value": 60},
                {"label": "thickness", "value": 10, "kind": "thickness"},
                {"label": "centre hole", "value": 8, "kind": "diameter"},
                {"label": "countersink at face", "value": 16, "kind": "diameter"},
                {"label": "countersink included angle", "value": 90, "kind": "angle"},
            ],
        }
        base.update(changes)
        return base

    def recipe(self, angle: float = 90) -> dict:
        return {
            "name": "Plate", "units": "mm",
            "parameters": [
                {"name": "plate_l", "value": 100},
                {"name": "plate_w", "value": 60},
                {"name": "plate_t", "value": 10},
                {"name": "spigot_d", "value": 8},
                {"name": "csink_d", "value": 16},
                {"name": "csink_inc", "value": angle, "unit": "deg"},
            ],
            "operations": [
                {"op": "sketch", "name": "Body", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0],
                     "width": "plate_l", "height": "plate_w"}]},
                {"op": "extrude", "name": "Plate", "sketch": "Body",
                 "distance": "plate_t"},
                {"op": "sketch", "name": "C", "plane": "xy", "entities": [
                    {"type": "point", "position": [0, 0]}]},
                {"op": "hole", "sketch": "C", "diameter": "spigot_d",
                 "through_all": True, "style": "countersink",
                 "csink_diameter": "csink_d", "csink_angle": "csink_inc"},
            ],
        }

    def check(self, reading: dict, recipe: dict) -> dict:
        parsed = PartRecipe.model_validate(recipe)
        rehearsal = rehearse(parsed)
        assert rehearsal["ok"], rehearsal["findings"]
        return compare(DrawingReading.model_validate(reading), rehearsal)

    def test_an_angle_parameter_is_not_reported_as_an_invented_length(self):
        """It used to come out as a 15.708 mm literal nobody had drawn."""
        result = self.check(self.reading(), self.recipe())
        assert result["invented"] == []
        assert result["ok"] is True

    def test_the_drawing_angle_is_matched_against_it(self):
        result = self.check(self.reading(), self.recipe())
        matched = {entry["drawing"] for entry in result["matched"]}
        assert "countersink included angle = 90.0 deg" in matched

    def test_a_model_with_the_wrong_angle_is_caught(self):
        result = self.check(self.reading(), self.recipe(angle=82))
        assert result["ok"] is False
        [missing] = [m for m in result["missing"] if "angle" in m["drawing"]]
        assert "No angle parameter" in missing["why"]

    def test_an_angle_the_model_never_declares_is_caught(self):
        recipe = self.recipe()
        recipe["parameters"] = [p for p in recipe["parameters"]
                                if p["name"] != "csink_inc"]
        recipe["operations"][-1]["csink_angle"] = "90 deg"
        result = self.check(self.reading(), recipe)
        assert result["ok"] is False
        assert any("angle" in m["drawing"] for m in result["missing"])


class TestACentreLineDimension:
    """A symmetric pitch reaches the model as a half-spacing."""

    def test_a_pitch_matches_the_half_the_model_drives(self):
        rehearsal = {
            "parameters": {"hole_pitch_half": 3.8},
            "parameter_dimensions": {"hole_pitch_half": "length"},
            "sketches": {},
            "result": {"span_mm": [100.0, 60.0, 10.0]},
        }
        reading = DrawingReading.model_validate({
            "units": "mm", "overall": [100, 60, 10],
            "dimensions": [{"label": "bolt pitch", "value": 76}],
        })
        result = compare(reading, rehearsal)
        assert result["ok"] is True
        [entry] = result["matched"]
        assert entry["as"] == "half of it, measured from the centre line"

    def test_it_does_not_match_anything_else(self):
        """Half is a convention, not a licence to accept any factor."""
        rehearsal = {
            "parameters": {"something": 2.5},
            "parameter_dimensions": {"something": "length"},
            "sketches": {},
            "result": {"span_mm": [100.0, 60.0, 10.0]},
        }
        reading = DrawingReading.model_validate({
            "units": "mm", "overall": [100, 60, 10],
            "dimensions": [{"label": "bolt pitch", "value": 76}],
        })
        assert compare(reading, rehearsal)["ok"] is False


class TestTheWorkedExample:
    """`examples/drawings/cover_plate.json` against `examples/cover_plate.json`.

    The pair is the documentation for this route: a reading anyone can compare
    with the recipe beside it. If the check ever stops passing, one of the two
    has drifted and the Skill is teaching something that does not work.
    """

    def result(self) -> dict:
        reading = DrawingReading.model_validate(
            json.loads((EXAMPLES / "drawings" / "cover_plate.json").read_text()))
        recipe = PartRecipe.model_validate(
            json.loads((EXAMPLES / "cover_plate.json").read_text()))
        rehearsal = rehearse(recipe)
        assert rehearsal["ok"], rehearsal["findings"]
        return compare(reading, rehearsal)

    def test_the_recipe_satisfies_the_drawing(self):
        result = self.result()
        assert result["ok"] is True, result["missing"]

    def test_every_driving_dimension_reaches_the_model(self):
        assert self.result()["missing"] == []

    def test_the_model_invents_nothing(self):
        assert self.result()["invented"] == []

    def test_the_derived_values_are_reported_as_derived(self):
        """A hole spacing computed from the plate and the margin is correct."""
        derived = {entry["model"] for entry in self.result().get("derived", [])}
        assert any("edge_margin" in name for name in derived)

    def test_there_is_nothing_to_warn_about(self):
        assert self.result()["warnings"] == []
