"""Rehearsing a drawing: the ledger, and the round trip against the part.

`drawing.py` reads a sheet somebody sent you; this is the other direction. The
thing worth testing is not that the ledger records what it was asked to record
-- it would be hard to get that wrong -- but the two faults it exists to find:

* a dimension the sheet states that the part does not realise;
* **a number the part states that the sheet never gives**, which is an
  under-dimensioned drawing and the one drawing fault that is invisible on the
  sheet, because every dimension that is there is correct.

Both come out of `drawing.compare`, reused rather than reimplemented, with only
its vocabulary turned round. So the tests below are largely about that
translation being faithful and about the checks not firing on a correct sheet.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from inventor_mcp.drafting import as_reading, rehearse_drawing
from inventor_mcp.schema import DrawingRecipe, PartRecipe

#: A 120 x 80 x 8 plate with a 6 mm bore. Four parameters, all of them driving
#: geometry, so a complete drawing of it states four numbers.
PART = PartRecipe.model_validate({
    "name": "Plate", "units": "mm",
    "parameters": [
        {"name": "plate_w", "value": 120},
        {"name": "plate_d", "value": 80},
        {"name": "plate_t", "value": 8},
        {"name": "hole_d", "value": 6},
    ],
    "operations": [
        {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
            {"type": "rectangle", "center": [0, 0],
             "width": "plate_w", "height": "plate_d"}]},
        {"op": "extrude", "name": "Body", "sketch": "Outline", "distance": "plate_t"},
        {"op": "sketch", "name": "Holes", "plane": "xy", "entities": [
            {"type": "point", "name": "p", "position": [0, 0]}]},
        {"op": "hole", "name": "Bore", "sketch": "Holes", "points": ["p"],
         "diameter": "hole_d", "through_all": True},
    ]})

COMPLETE = {
    "name": "PlateDrawing", "template": "iso.idw", "views": [
        {"name": "FRONT", "direction": "front", "dimension": ["plate_w", "plate_t"]},
        {"name": "TOP", "direction": "top", "at": [0, 140],
         "dimension": ["plate_d", "hole_d"]},
    ]}


def draw(drawing, part=PART):
    return rehearse_drawing(DrawingRecipe.model_validate(drawing), part)


def warnings_of(report):
    return [warning["warning"] for warning in report["warnings"]]


class TestTheLedgerResolvesWhatTheSheetWouldSay:
    def test_a_parameter_name_becomes_its_number(self):
        report = draw(COMPLETE)
        front = report["ledger"]["views"][0]
        assert front["name"] == "FRONT"
        assert [(d["label"], d["value"]) for d in front["dimensions"]] == [
            ("plate_w", 120.0), ("plate_t", 8.0)]

    def test_the_expression_is_kept_beside_the_value(self):
        """The `Resolved` reuse: a dimension knows what produced it."""
        report = draw({"name": "D", "template": "t.idw", "views": [
            {"name": "FRONT", "dimension": ["plate_w - 2 * hole_d"]}]})
        entry = report["ledger"]["views"][0]["dimensions"][0]
        assert entry["value"] == pytest.approx(108.0)
        assert "plate_w" in entry["expression"]

    def test_the_sheets_units_govern_and_not_the_parts(self):
        """A sheet may be dimensioned in inches from a part modelled in mm."""
        report = draw({"name": "D", "units": "in", "template": "t.idw", "views": [
            {"name": "FRONT", "dimension": ["plate_w"]}]})
        # 120 mm is 4.7244 inches.
        assert report["ledger"]["views"][0]["dimensions"][0]["value"] == pytest.approx(
            4.724409, abs=5e-6)
        assert report["ledger"]["units"] == "in"

    def test_an_angle_is_recognised_without_being_declared(self):
        part = PartRecipe.model_validate({
            "name": "Wedge", "units": "mm",
            "parameters": [{"name": "block", "value": 40},
                           {"name": "pull", "value": "3 deg"}],
            "operations": [
                {"op": "sketch", "name": "O", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0],
                     "width": "block", "height": "block"}]},
                {"op": "extrude", "name": "B", "sketch": "O", "distance": 20,
                 "taper": "pull"},
            ]})
        report = draw({"name": "D", "template": "t.idw", "views": [
            {"name": "FRONT", "dimension": ["block", "pull"]}]}, part)
        kinds = {d["label"]: d["kind"]
                 for d in report["ledger"]["views"][0]["dimensions"]}
        assert kinds == {"block": "linear", "pull": "angle"}

    def test_a_reference_dimension_is_marked_as_one(self):
        report = draw({"name": "D", "template": "t.idw", "views": [
            {"name": "FRONT", "dimension": ["plate_w"],
             "reference": ["plate_w - 2 * hole_d"]}]})
        marked = {d["label"]: d["reference"]
                  for d in report["ledger"]["views"][0]["dimensions"]}
        assert marked == {"plate_w": False, "plate_w - 2 * hole_d": True}

    def test_the_scale_resolves_like_any_other_number(self):
        report = draw({"name": "D", "template": "t.idw", "views": [
            {"name": "FRONT", "scale": "1 / 2", "dimension": ["plate_w"]}]})
        assert report["ledger"]["views"][0]["scale"] == pytest.approx(0.5)


class TestTheUnderDimensionedSheet:
    """The fault this module is for, and the one you cannot see by looking."""

    def test_a_forgotten_thickness_is_reported(self):
        report = draw({"name": "D", "template": "t.idw", "views": [
            {"name": "TOP", "direction": "top",
             "dimension": ["plate_w", "plate_d", "hole_d"]}]})
        assert report["round_trip"]["undimensioned_count"] == 1
        assert report["round_trip"]["undimensioned"][0]["model"] == "parameter plate_t"

    def test_it_is_surfaced_in_warnings_and_not_only_in_the_round_trip(self):
        """`warnings` is the field a caller reads first."""
        report = draw({"name": "D", "template": "t.idw", "views": [
            {"name": "TOP", "direction": "top",
             "dimension": ["plate_w", "plate_d", "hole_d"]}]})
        assert any("not dimensioned" in text for text in warnings_of(report))

    def test_the_axis_nobody_stated_is_reported_too(self):
        """A second, independent reading of the same defect: the part is 8 mm
        thick on Z and no dimension on the sheet gives 8."""
        report = draw({"name": "D", "template": "t.idw", "views": [
            {"name": "TOP", "direction": "top",
             "dimension": ["plate_w", "plate_d", "hole_d"]}]})
        assert any("overall Z" in text for text in warnings_of(report))

    def test_it_stays_a_warning_rather_than_a_failure(self):
        """It can be deliberate -- a value a general note covers -- and a check
        that refused a legitimate sheet would teach the reader to ignore it."""
        report = draw({"name": "D", "template": "t.idw", "views": [
            {"name": "TOP", "direction": "top", "dimension": ["plate_w"]}]})
        assert report["ok"] is True
        assert report["round_trip"]["undimensioned_count"] == 3

    def test_a_complete_sheet_is_not_warned_about(self):
        """A warning that fires on a correct drawing teaches the reader to ignore it."""
        report = draw(COMPLETE)
        assert report["ok"] is True
        assert report["round_trip"]["undimensioned_count"] == 0
        assert warnings_of(report) == []

    def test_a_derived_value_is_not_counted_as_undimensioned(self):
        """A parametric part is supposed to derive numbers, and a drawing need
        not state every intermediate."""
        part = PartRecipe.model_validate({
            "name": "Plate", "units": "mm",
            "parameters": [
                {"name": "plate_w", "value": 120},
                {"name": "plate_d", "value": 80},
                {"name": "plate_t", "value": 8},
                {"name": "margin", "value": "plate_w / 12"},
            ],
            "operations": [
                {"op": "sketch", "name": "O", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0],
                     "width": "plate_w", "height": "plate_d"}]},
                {"op": "extrude", "name": "B", "sketch": "O", "distance": "plate_t"},
                {"op": "sketch", "name": "H", "plane": "xy", "entities": [
                    {"type": "point", "name": "p", "position": ["margin", 0]}]},
                {"op": "hole", "name": "Bore", "sketch": "H", "points": ["p"],
                 "diameter": 6, "through_all": True},
            ]})
        report = draw({"name": "D", "template": "t.idw", "views": [
            {"name": "FRONT", "dimension": ["plate_w", "plate_t"]},
            {"name": "TOP", "direction": "top", "at": [0, 140],
             "dimension": ["plate_d"]}]}, part)
        undimensioned = [e["model"] for e in report["round_trip"]["undimensioned"]]
        assert "parameter margin" not in undimensioned
        assert report["round_trip"].get("derived")


class TestASheetThatStatesWhatThePartHasNot:
    def test_an_expression_the_part_does_not_realise_is_reported(self):
        """A driving dimension of 100 on a part that has no such number."""
        report = draw({"name": "D", "template": "t.idw", "views": [
            {"name": "FRONT", "dimension": [
                "plate_w", "plate_d", "plate_t", "hole_d", "plate_w - 20"]}]})
        stated = report["round_trip"]["states_what_the_part_does_not_have"]
        assert stated and "100" in stated[0]["drawing"]
        assert report["ok"] is False

    def test_the_same_value_as_a_reference_dimension_is_not_reported(self):
        """A reference dimension restates something fixed elsewhere and drives
        nothing, so it is not held against the part."""
        report = draw({"name": "D", "template": "t.idw", "views": [
            {"name": "FRONT",
             "dimension": ["plate_w", "plate_d", "plate_t", "hole_d"],
             "reference": ["plate_w - 20"]}]})
        assert report["round_trip"]["states_what_the_part_does_not_have"] == []
        assert report["ok"] is True


class TestItRefusesRatherThanDrawingNonsense:
    def test_a_parameter_the_part_does_not_declare_is_a_finding(self):
        report = draw({"name": "D", "views": [
            {"name": "FRONT", "dimension": ["plate_wdith"]}]})
        assert report["ok"] is False
        assert "plate_wdith" in report["findings"][0]["error"]

    def test_a_part_that_does_not_build_stops_the_drawing(self):
        broken = PartRecipe.model_validate({
            "name": "Broken", "units": "mm", "operations": [
                {"op": "extrude", "name": "E", "sketch": "Nope", "distance": 5}]})
        report = draw(COMPLETE, broken)
        assert report["ok"] is False
        assert report["rehearsed_the_part"] is False
        assert "nothing to draw" in report["findings"][0]["error"]
        assert "round_trip" not in report

    def test_a_sheet_with_no_template_is_warned_about(self):
        report = draw({"name": "D", "views": [
            {"name": "FRONT", "dimension": ["plate_w", "plate_d", "plate_t", "hole_d"]}]})
        assert any("title block" in text for text in warnings_of(report))

    def test_a_view_that_dimensions_nothing_is_warned_about(self):
        report = draw({"name": "D", "template": "t.idw", "views": [
            {"name": "FRONT", "dimension": ["plate_w", "plate_d", "plate_t", "hole_d"]},
            {"name": "RIGHT", "direction": "right", "at": [200, 0]}]})
        assert any("dimension nothing" in text for text in warnings_of(report))

    def test_an_iso_view_is_allowed_to_dimension_nothing(self):
        """Dimensioning an isometric view is bad practice, so an empty one is
        the normal case rather than an oversight."""
        report = draw({"name": "D", "template": "t.idw", "views": [
            {"name": "FRONT", "dimension": ["plate_w", "plate_d", "plate_t", "hole_d"]},
            {"name": "ISO", "direction": "iso", "at": [200, 0]}]})
        assert warnings_of(report) == []


class TestTheReadingItProduces:
    """It has to be a `DrawingReading` the reading side would accept, because
    that is the whole reason `compare` can be reused."""

    def test_the_generated_reading_validates_and_carries_the_dimensions(self):
        report = draw(COMPLETE)
        reading = as_reading(DrawingRecipe.model_validate(COMPLETE), report["ledger"])
        assert reading.projection == "third_angle"
        assert [d.label for d in reading.dimensions] == [
            "plate_w", "plate_t", "plate_d", "hole_d"]
        assert [v.kind for v in reading.views] == ["front", "top"]

    def test_the_views_carry_no_extent_on_purpose(self):
        """An extent is what a view shows, and the only source for it here is
        the part -- so filling it in would have the overall-size check compare
        the part against itself and pass always."""
        report = draw(COMPLETE)
        reading = as_reading(DrawingRecipe.model_validate(COMPLETE), report["ledger"])
        assert all(view.extent is None for view in reading.views)
        assert reading.overall is None

    def test_a_sheet_dimensioning_nothing_still_produces_a_valid_reading(self):
        """`DrawingReading` refuses a reading with no dimensions and nothing
        marked unreadable, which a produced sheet has to satisfy too."""
        recipe = DrawingRecipe.model_validate({"name": "D", "views": [
            {"name": "ISO", "direction": "iso"}]})
        report = rehearse_drawing(recipe, PART)
        reading = as_reading(recipe, report["ledger"])
        assert reading.dimensions == []
        assert reading.unreadable

    def test_the_projection_warning_of_the_reading_side_is_dropped(self):
        """A produced sheet always has a projection angle -- the schema has no
        'unknown' -- so that warning cannot apply and would only confuse."""
        report = draw(COMPLETE)
        assert not any("projection" in warning.get("warning", "")
                       for warning in report["round_trip"]["warnings"])

class TestTheShippedDrawing:
    """`examples/drawings/` against `examples/`, both executable.

    Pointed at a whole shipped recipe rather than the fixture above, for the
    reason `ARCHITECTURE.md` gives about the examples: two bugs were found by
    writing them rather than by writing tests. A drawing schema nobody has
    aimed at a real eleven-parameter part is a schema whose gaps are still
    hiding.
    """

    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def paired(self, stem: str):
        drawing = DrawingRecipe.model_validate(json.loads(
            (self.ROOT / "examples" / "drawings" / f"{stem}.json").read_text(
                encoding="utf-8")))
        part = PartRecipe.model_validate(json.loads(
            (self.ROOT / "examples" / f"{stem}.json").read_text(encoding="utf-8")))
        return drawing, part

    def test_every_shipped_drawing_names_a_shipped_part(self):
        """Otherwise it is a drawing of nothing and nothing can check it."""
        drawings = sorted((self.ROOT / "examples" / "drawings").glob("*.json"))
        assert drawings, "no drawings shipped"
        for path in drawings:
            assert (self.ROOT / "examples" / path.name).exists(), (
                f"{path.name} has no part of the same name in examples/")

    def test_the_mounting_plates_drawing_dimensions_all_of_it(self):
        drawing, part = self.paired("mounting_plate")
        report = rehearse_drawing(drawing, part)
        assert report["ok"] is True, report["findings"]
        assert warnings_of(report) == []
        assert report["round_trip"]["undimensioned_count"] == 0

    def test_the_hole_pitches_are_derived_rather_than_undimensioned(self):
        """The part drives its holes from `edge_margin`, and the sheet states
        that -- so the 96 mm pitch the part computes is derived from numbers the
        drawing gives, which is what a parametric model is supposed to do.

        A drawing that stated the pitch instead and left out the margin would
        pin the same holes and be reported as leaving the margin undimensioned.
        Both are defensible on paper; only one matches the model, and that is
        the sort of thing this check exists to make visible.
        """
        drawing, part = self.paired("mounting_plate")
        derived = {entry["model"]
                   for entry in rehearse_drawing(drawing, part)["round_trip"]["derived"]}
        assert any("plate_w - 2 * edge_margin" in name for name in derived)
