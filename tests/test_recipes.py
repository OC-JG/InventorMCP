"""Recipe validation, building, and the parametric edit loop."""

from __future__ import annotations

import json
import math
import pathlib

import pytest
from pydantic import ValidationError

from inventor_mcp.builder import build_part, check_recipe, resolve_axis
from inventor_mcp.schema import PartRecipe


def build(session, recipe: dict):
    return build_part(session, PartRecipe.model_validate(recipe))


class TestSchemaValidation:
    def test_unknown_field_is_rejected(self):
        with pytest.raises(ValidationError, match="thicknes"):
            PartRecipe.model_validate({
                "name": "X",
                "operations": [{"op": "sketch", "entities": []}],
                "thicknes": 4,
            })

    def test_unknown_operation_is_rejected(self):
        with pytest.raises(ValidationError):
            PartRecipe.model_validate({"name": "X", "operations": [{"op": "bevel"}]})

    def test_a_recipe_needs_an_operation(self):
        with pytest.raises(ValidationError, match="at least one operation"):
            PartRecipe.model_validate({"name": "X", "operations": []})

    def test_duplicate_parameters_are_rejected(self):
        with pytest.raises(ValidationError, match="Duplicate parameter"):
            PartRecipe.model_validate({
                "name": "X",
                "parameters": [{"name": "w", "value": 1}, {"name": "W", "value": 2}],
                "operations": [{"op": "sketch", "entities": []}],
            })

    def test_parameter_names_must_be_identifiers(self):
        with pytest.raises(ValidationError, match="letters, digits"):
            PartRecipe.model_validate({
                "name": "X",
                "parameters": [{"name": "plate width", "value": 1}],
                "operations": [{"op": "sketch", "entities": []}],
            })

    def test_extrude_needs_a_distance(self):
        with pytest.raises(ValidationError, match="`distance` is required"):
            PartRecipe.model_validate({
                "name": "X",
                "operations": [{"op": "extrude", "extent": "distance"}],
            })

    def test_counterbore_needs_its_dimensions(self):
        with pytest.raises(ValidationError, match="cbore_diameter"):
            PartRecipe.model_validate({
                "name": "X",
                "operations": [{"op": "hole", "diameter": 6, "style": "counterbore"}],
            })

    def test_within_requires_near(self):
        with pytest.raises(ValidationError, match="`within` requires `near`"):
            PartRecipe.model_validate({
                "name": "X",
                "operations": [{"op": "fillet", "radius": 2, "edges": {"within": 5}}],
            })


class TestStaticChecks:
    def test_a_good_recipe_passes(self, plate_recipe):
        report = check_recipe(PartRecipe.model_validate(plate_recipe))
        assert report["ok"], report["findings"]
        assert report["sketches"]["Body"]["profiles"] == 1

    def test_undefined_parameter_is_found_without_inventor(self, plate_recipe):
        plate_recipe["operations"][1]["distance"] = "thicknes"
        report = check_recipe(PartRecipe.model_validate(plate_recipe))
        assert not report["ok"]
        assert "Unknown parameter" in report["findings"][0]["error"]

    def test_extruding_a_sketch_that_does_not_exist(self, plate_recipe):
        plate_recipe["operations"][1]["sketch"] = "Bodyy"
        report = check_recipe(PartRecipe.model_validate(plate_recipe))
        assert any("no earlier operation creates" in f["error"] for f in report["findings"])

    def test_open_profile_is_reported(self):
        recipe = PartRecipe.model_validate({
            "name": "Open",
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "polyline", "points": [[0, 0], [10, 0], [10, 10]], "closed": False}]},
                {"op": "extrude", "sketch": "S", "distance": 5},
            ],
        })
        report = check_recipe(recipe)
        assert any("No closed profile" in f["error"] for f in report["findings"])

    def test_hole_sketch_without_points_is_reported(self):
        recipe = PartRecipe.model_validate({
            "name": "NoPoints",
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "circle", "diameter": 10}]},
                {"op": "hole", "sketch": "S", "diameter": 5},
            ],
        })
        report = check_recipe(recipe)
        assert any("hole-centre points" in f["error"] for f in report["findings"])

    def test_a_length_parameter_used_as_an_angle_is_caught(self):
        recipe = PartRecipe.model_validate({
            "name": "Bad",
            "parameters": [{"name": "w", "value": 10}],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": "w", "height": "w"}]},
                {"op": "extrude", "sketch": "S", "distance": "30 deg"},
            ],
        })
        report = check_recipe(recipe)
        assert any("must be a length" in f["error"] for f in report["findings"])

    def test_duplicate_feature_names_are_caught(self, plate_recipe):
        plate_recipe["operations"][2]["name"] = "Plate"
        report = check_recipe(PartRecipe.model_validate(plate_recipe))
        assert any("both named" in f["error"] for f in report["findings"])


class TestBuilding:
    def test_the_plate_builds_with_the_expected_size(self, session, plate_recipe):
        result = build(session, plate_recipe)
        assert result["ok"], result["errors"]
        box = result["mass_properties"]["bounding_box"]
        assert (box[3] - box[0], box[4] - box[1], box[5] - box[2]) == pytest.approx((12.0, 8.0, 0.8))

    def test_volume_accounts_for_the_holes_and_fillets(self, session, plate_recipe):
        result = build(session, plate_recipe)
        slab = 12.0 * 8.0 * 0.8
        holes = 4 * math.pi * 0.33**2 * 0.8
        assert result["mass_properties"]["volume"] == pytest.approx(slab - holes, rel=0.02)

    def test_dimensions_are_stored_as_expressions(self, session, plate_recipe):
        result = build(session, plate_recipe)
        extrude = next(op for op in result["operations"] if op["op"] == "extrude")
        assert extrude["detail"]["distance"]["expression"] == "thk"

    def test_parameters_reach_the_document(self, session, plate_recipe):
        result = build(session, plate_recipe)
        names = {p["name"] for p in result["parameters"]}
        assert names == {"plate_w", "plate_d", "thk", "hole_d", "edge_margin", "corner_r"}

    def test_a_derived_parameter_can_reference_an_earlier_one(self, session, plate_recipe):
        plate_recipe["parameters"].append(
            {"name": "hole_spacing", "value": "plate_w - 2 * edge_margin"}
        )
        result = build(session, plate_recipe)
        derived = next(p for p in result["parameters"] if p["name"] == "hole_spacing")
        assert derived["value"] == pytest.approx(96.0)

    def test_a_failing_operation_stops_the_build_and_says_where(self, session, plate_recipe):
        plate_recipe["operations"][2]["edges"] = {"filter": "circular", "feature": "Nope"}
        result = build(session, plate_recipe)
        assert not result["ok"]
        assert result["stopped_at"].startswith("operation 2")

    def test_continuing_past_errors_reports_them_all(self, session, plate_recipe):
        plate_recipe["operations"][2]["radius"] = "-5 mm"
        recipe = PartRecipe.model_validate(plate_recipe)
        result = build_part(session, recipe, stop_on_error=False)
        assert not result["ok"]
        assert len(result["operations"]) == 4  # everything except the bad fillet

    def test_reserved_parameter_names_are_refused(self, session, plate_recipe):
        plate_recipe["parameters"].append({"name": "sqrt", "value": 5})
        result = build(session, plate_recipe)
        assert not result["ok"]
        assert "reserved" in result["errors"][0]["error"]


class TestRevolveAndAxes:
    def _recipe(self, axis: str) -> dict:
        return {
            "name": "Shaft",
            "units": "mm",
            "parameters": [{"name": "dia", "value": 20}, {"name": "length", "value": 60}],
            "operations": [
                {"op": "sketch", "name": "Profile", "plane": "xz", "entities": [
                    {"type": "line", "start": [0, 0], "end": [0, 60], "name": "axis",
                     "construction": True, "centerline": True},
                    {"type": "rectangle", "corner": [0, 0], "width": 10, "height": 60},
                ]},
                {"op": "revolve", "sketch": "Profile", "axis": axis},
            ],
        }

    def test_revolving_about_a_named_sketch_line(self, session):
        result = build(session, self._recipe("axis"))
        assert result["ok"], result["errors"]
        assert result["operations"][-1]["kind"] == "revolve"

    def test_revolving_about_an_origin_axis(self, session):
        assert build(session, self._recipe("z"))["ok"]

    def test_an_unknown_axis_names_the_alternatives(self, session):
        result = build(session, self._recipe("centre_line"))
        assert not result["ok"]
        assert "Cannot resolve" in result["errors"][0]["error"]

    def test_resolve_axis_prefers_the_named_sketch(self, session):
        build(session, self._recipe("axis"))
        context = session.context()
        spec = resolve_axis(context, "axis", "Profile")
        assert spec.kind == "sketch_line" and spec.sketch == "Profile"


class TestUnitsInRecipes:
    def test_an_imperial_recipe_produces_imperial_expressions(self, session):
        recipe = {
            "name": "Imperial", "units": "in",
            "parameters": [{"name": "w", "value": 2}],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": "w", "height": 1}]},
                {"op": "extrude", "sketch": "S", "distance": 0.25},
            ],
        }
        result = build(session, recipe)
        assert result["ok"], result["errors"]
        extrude = next(op for op in result["operations"] if op["op"] == "extrude")
        assert extrude["detail"]["distance"]["expression"] == "0.25 in"
        assert extrude["detail"]["distance"]["value"] == pytest.approx(0.635)

    def test_units_may_be_mixed_inside_an_expression(self, session):
        recipe = {
            "name": "Mixed", "units": "mm",
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": "1 in + 6 mm", "height": 20}]},
                {"op": "extrude", "sketch": "S", "distance": 5},
            ],
        }
        result = build(session, recipe)
        assert result["ok"], result["errors"]
        box = result["mass_properties"]["bounding_box"]
        assert box[3] - box[0] == pytest.approx(3.14)

    def test_an_angle_parameter_keeps_its_dimension(self, session):
        recipe = {
            "name": "Tapered", "units": "mm",
            "parameters": [{"name": "draft", "value": 3, "unit": "deg"}],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": 40, "height": 40}]},
                {"op": "extrude", "sketch": "S", "distance": 10, "taper": "draft"},
            ],
        }
        result = build(session, recipe)
        assert result["ok"], result["errors"]
        assert result["parameters"][0]["units"] == "deg"


class TestRehearsal:
    """A recipe can be valid and still build the wrong part.

    Static checks catch a malformed recipe. They cannot catch a well-formed one
    whose cut misses the material or whose parameters drive nothing -- and both
    of those reached a live Inventor before anyone noticed. The simulator can
    see them in milliseconds on any machine, so validation builds the thing.
    """

    def rehearse(self, recipe):
        from inventor_mcp.builder import rehearse
        from inventor_mcp.schema import PartRecipe

        return rehearse(PartRecipe.model_validate(recipe))

    PLATE = {
        "name": "Plate", "units": "mm",
        "parameters": [{"name": "size", "value": 40}, {"name": "thk", "value": 10}],
        "operations": [
            {"op": "sketch", "name": "Base", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": "size", "height": "size"}]},
            {"op": "extrude", "name": "Body", "sketch": "Base", "distance": "thk"},
        ],
    }

    def with_extra(self, *operations):
        import copy

        recipe = copy.deepcopy(self.PLATE)
        recipe["operations"].extend(operations)
        return recipe

    def test_a_good_recipe_rehearses_clean(self):
        report = self.rehearse(self.PLATE)
        assert report["ok"] is True
        assert report["rehearsed"] is True
        assert report["warnings"] == []

    def test_it_reports_what_each_operation_moved(self):
        report = self.rehearse(self.PLATE)
        [extrude] = [s for s in report["steps"] if s["op"] == "extrude"]
        assert extrude["measured"]["volume_cm3"] == pytest.approx(16.0)

    def test_and_the_finished_part(self):
        report = self.rehearse(self.PLATE)
        assert report["result"]["volume_cm3"] == pytest.approx(16.0)
        assert report["result"]["span_mm"] == [40.0, 40.0, 10.0]

    def test_a_cut_that_misses_the_part_is_caught(self):
        """The bracket's slots cut empty air for three live rounds."""
        report = self.rehearse(self.with_extra(
            {"op": "sketch", "name": "Miss", "plane": "xy", "entities": [
                {"type": "circle", "center": [500, 0], "diameter": 10}]},
            {"op": "extrude", "sketch": "Miss", "distance": 5, "operation": "cut"},
        ))
        assert any("does not reach the part" in w["warning"] for w in report["warnings"])

    def test_a_cut_that_lands_on_the_part_is_not_complained_about(self):
        report = self.rehearse(self.with_extra(
            {"op": "sketch", "name": "Hit", "plane": "xy", "entities": [
                {"type": "circle", "center": [0, 0], "diameter": 10}]},
            {"op": "extrude", "sketch": "Hit", "distance": 5, "operation": "cut"},
        ))
        assert report["warnings"] == []

    def test_a_cut_just_touching_the_edge_is_allowed(self):
        """Cutting flush with a face is a legitimate design, not a mistake."""
        report = self.rehearse(self.with_extra(
            {"op": "sketch", "name": "Edge", "plane": "xy", "entities": [
                {"type": "circle", "center": [20, 0], "diameter": 10}]},
            {"op": "extrude", "sketch": "Edge", "distance": 5, "operation": "cut"},
        ))
        assert report["warnings"] == []

    def test_a_parameter_that_drives_nothing_is_named(self):
        import copy

        frozen = copy.deepcopy(self.PLATE)
        frozen["operations"][0]["entities"][0]["width"] = 40
        frozen["operations"][0]["entities"][0]["height"] = 40
        frozen["operations"][1]["distance"] = 10
        report = self.rehearse(frozen)
        [warning] = [w for w in report["warnings"] if "drive nothing" in w["warning"]]
        assert "size" in warning["warning"] and "thk" in warning["warning"]

    def test_a_parameter_used_only_through_another_parameter_counts_as_driving(self):
        import copy

        recipe = copy.deepcopy(self.PLATE)
        recipe["parameters"].append({"name": "half", "value": "size / 2"})
        recipe["operations"].append(
            {"op": "sketch", "name": "Hole", "plane": "xy", "entities": [
                {"type": "point", "position": ["half", 0]}]})
        recipe["operations"].append(
            {"op": "hole", "sketch": "Hole", "diameter": 5, "depth": 5})
        assert not [w for w in self.rehearse(recipe)["warnings"]
                    if "drive nothing" in w["warning"]]

    def test_a_recipe_that_does_not_build_says_where_it_stopped(self):
        report = self.rehearse(self.with_extra(
            {"op": "fillet", "edges": {"feature": "Nothing"}, "radius": 2}))
        assert report["ok"] is False
        assert report["rehearsed"] is True
        assert "operation 2" in report["findings"][-1]["where"]

    def test_a_malformed_recipe_never_reaches_the_rehearsal(self):
        import copy

        broken = copy.deepcopy(self.PLATE)
        broken["operations"][1]["distance"] = "no_such_parameter"
        report = self.rehearse(broken)
        assert report["ok"] is False
        assert report["rehearsed"] is False
        assert "Fix the findings first" in report["hint"]

    def test_a_thread_designation_is_not_read_as_an_expression(self):
        """M5x0.8 refers to no parameter; it must not raise."""
        report = self.rehearse(self.with_extra(
            {"op": "sketch", "name": "Tap", "plane": "xy", "entities": [
                {"type": "point", "position": [0, 0]}]},
            {"op": "hole", "sketch": "Tap", "diameter": 5, "depth": 6, "tap": "M5x0.8"},
        ))
        assert report["ok"] is True

    def test_a_fillet_is_not_judged_on_which_way_the_volume_went(self):
        """The simulator models every fillet as subtractive; a real inside
        corner adds material, and warning about that would cry wolf."""
        report = self.rehearse(self.with_extra(
            {"op": "fillet", "edges": {"filter": "vertical"}, "radius": 3}))
        assert report["warnings"] == []

    @pytest.mark.parametrize("path", sorted(
        (pathlib.Path(__file__).resolve().parent.parent / "examples").glob("*.json")),
        ids=lambda p: p.stem)
    def test_every_shipped_example_rehearses_without_a_warning(self, path):
        report = self.rehearse(json.loads(path.read_text()))
        assert report["ok"] is True
        assert report["warnings"] == [], (
            f"{path.stem}: {[w['warning'] for w in report['warnings']]}")
