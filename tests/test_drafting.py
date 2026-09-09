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
        # The recipe's `front` and `top`, as a *reading* would label them. The
        # two vocabularies differ and this is where they are translated:
        # Inventor's front shows the XY plane, which a sheet labels the plan.
        # Measured on 2027.1, 2026-09-08; `drafting._VIEW_KINDS` carries it.
        assert [v.kind for v in reading.views] == ["top", "front"]

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


class TestBuildingTheDrawing:
    """The round trip through a backend, which is what `build_drawing` adds.

    `rehearse_drawing` computes what a sheet would say. This makes one and then
    reads it back, and the difference is the whole value: a dimension the
    retrieval could not find is absent from the check rather than assumed
    present, and a view's direction is asked of the sheet rather than
    remembered from the request.

    Only the simulator's half is testable here. The COM half has never run --
    `docs/INVENTOR_SETUP.md` has what a live seat must settle, and the first
    thing on that list is whether a retrieved dimension can be asked which model
    parameter it came from, because the whole approach rests on it.
    """

    def build(self, session, drawing, part=PART, **kwargs):
        from inventor_mcp.drafting import build_drawing

        return build_drawing(session, DrawingRecipe.model_validate(drawing),
                             part, **kwargs)

    def test_the_views_are_placed_and_measured_from_the_part(self, session):
        """A 120 x 80 x 8 plate: its `front` view spans 120 by 80 and its
        `top` view 120 by 8.

        Which reads backwards and is what Inventor does. Its view names are
        Y-up -- `front` looks down Z and shows the XY plane -- and a recipe's
        `direction` follows them, so a plate modelled flat on XY has its plan
        as its front view. Measured on 2027.1, 2026-09-08, by placing one base
        view per direction; `base.VIEW_AXES` carries the table and
        `docs/DECISIONS.md` the choice. The two extents differ, which is what
        makes this a check rather than a coincidence."""
        report = self.build(session, COMPLETE)
        placed = {entry["view"]["name"]: entry["view"] for entry in report["views"]}
        assert placed["FRONT"]["extent"] == pytest.approx([12.0, 8.0])
        assert placed["TOP"]["extent"] == pytest.approx([12.0, 0.8])

    def test_a_views_scale_shrinks_what_it_spans(self, session):
        report = self.build(session, {"name": "D", "template": "t.idw", "views": [
            {"name": "FRONT", "scale": 0.5, "dimension": ["plate_w"]}]})
        assert report["views"][0]["view"]["extent"] == pytest.approx([6.0, 4.0])

    def test_the_dimensions_are_retrieved_by_parameter(self, session):
        report = self.build(session, COMPLETE)
        retrieved = [entry["parameter"]
                     for view in report["views"] for entry in view["dimensions"]]
        assert retrieved == ["plate_w", "plate_t", "plate_d", "hole_d"]

    def test_a_retrieved_dimension_carries_the_models_own_value(self, session):
        """Which is the property that makes reading the sheet back a check on
        the recipe rather than on the arithmetic: the number on the sheet came
        from the model, so it cannot disagree with it."""
        report = self.build(session, COMPLETE)
        first = report["views"][0]["dimensions"][0]
        assert first["value"] == pytest.approx(12.0)  # 120 mm, in cm
        assert first["expression"] == "plate_w"

    def test_the_sheet_is_read_back_and_the_round_trip_reconciles(self, session):
        report = self.build(session, COMPLETE)
        assert report["ok"] is True
        assert len(report["read_back"]["views"]) == 2
        assert len(report["read_back"]["dimensions"]) == 4
        assert report["round_trip"]["undimensioned_count"] == 0

    def test_a_parameter_with_no_model_dimension_cannot_be_retrieved(self, session):
        """The check that only exists because the sheet is read back.

        Inventor can retrieve a dimension only if the model holds one, so a
        parameter driving neither a sketch dimension nor a feature value has
        nothing to retrieve -- and no static check could know that, because the
        parameter exists and resolves perfectly well.
        """
        part = PartRecipe.model_validate({
            "name": "Plate", "units": "mm",
            "parameters": [
                {"name": "plate_w", "value": 120},
                {"name": "plate_t", "value": 8},
                {"name": "spare", "value": 3, "comment": "drives nothing, on purpose"},
            ],
            "operations": [
                {"op": "sketch", "name": "O", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0],
                     "width": "plate_w", "height": 80}]},
                {"op": "extrude", "name": "B", "sketch": "O", "distance": "plate_t"},
            ]})
        report = self.build(session, {"name": "D", "template": "t.idw", "views": [
            {"name": "FRONT", "dimension": ["plate_w", "plate_t", "spare"]}]}, part)
        assert any("not on the sheet: spare" in warning["warning"]
                   for warning in report["warnings"])
        placed = [entry["parameter"] for entry in report["views"][0]["dimensions"]]
        assert placed == ["plate_w", "plate_t"]

    def test_a_parameter_driving_a_feature_is_retrievable(self, session):
        """`plate_t` drives an extrude's distance and not a sketch dimension.

        Inventor retrieves feature dimensions as readily as sketch ones -- an
        extrude's distance is a parameter in the model browser. Looking only at
        sketches was this simulator's first answer and it reported a plate's
        thickness as impossible to dimension, which is the one dimension a
        plate drawing certainly carries.
        """
        report = self.build(session, {"name": "D", "template": "t.idw", "views": [
            {"name": "FRONT", "dimension": ["plate_t"]}]})
        assert [e["parameter"] for e in report["views"][0]["dimensions"]] == ["plate_t"]
        assert report["views"][0]["dimensions"][0]["value"] == pytest.approx(0.8)

    def test_a_part_that_does_not_build_stops_before_any_sheet_is_made(self, session):
        broken = PartRecipe.model_validate({
            "name": "Broken", "units": "mm", "operations": [
                {"op": "extrude", "name": "E", "sketch": "Nope", "distance": 5}]})
        report = self.build(session, COMPLETE, broken)
        assert report["ok"] is False
        assert "nothing to draw" in report["findings"][0]["error"]
        assert "document" not in report

    def test_an_already_open_part_is_drawn_without_rebuilding_it(self, session):
        from inventor_mcp.builder import build_part

        built = build_part(session, PART)
        report = self.build(session, COMPLETE, part_doc_id=built["document"])
        assert report["part"]["reused"] is True
        assert report["ok"] is True

    def test_the_overall_size_check_has_extents_to_work_from(self, session):
        """Unlike the ledger's reading, a built sheet's views carry a size --
        so `compare`'s cheapest check is live rather than skipped."""
        from inventor_mcp.drafting import reading_of

        report = self.build(session, COMPLETE)
        session.backend.read_drawing(report["document"])
        reading = reading_of(
            DrawingRecipe.model_validate(COMPLETE),
            session.backend.read_drawing(report["document"]))
        # The recipe asked for FRONT then TOP. A recipe's `front` is Inventor's,
        # which shows XY, so the reading labels that view the plan and carries
        # 120 x 80; the recipe's `top` shows XZ and reads as the elevation at
        # 120 x 8. The pair is what `_overall_from` reconstructs the bounding
        # box from, so the kind and the extent have to agree -- which is the
        # whole reason `_as_read` translates both together.
        assert [view.kind for view in reading.views] == ["top", "front"]
        assert [view.extent for view in reading.views] == [
            pytest.approx([120.0, 80.0]), pytest.approx([120.0, 8.0])]
        assert not any("overall size" in warning.get("warning", "")
                       for warning in report["round_trip"]["warnings"])


class TestTheBackendContract:
    """Both backends implement the drawing methods, which the ABC enforces.

    Not a test of behaviour so much as a record of why the two cannot drift: a
    backend missing one of these will not instantiate, which is the property
    `ARCHITECTURE.md` calls construction rather than discipline.
    """

    def test_the_four_drawing_methods_are_abstract(self):
        from inventor_mcp.backend.base import Backend

        for name in ("new_drawing", "place_view", "retrieve_dimensions",
                     "read_drawing"):
            assert getattr(Backend, name).__isabstractmethod__, name

    def test_a_backend_missing_one_will_not_instantiate(self):
        """Re-declared as abstract rather than deleted, because an inherited
        method cannot be deleted -- and re-declaring is what a half-written
        backend looks like anyway."""
        import abc

        from inventor_mcp.backend.mock.backend import MockBackend

        class Forgetful(MockBackend):
            @abc.abstractmethod
            def read_drawing(self, doc_id):  # pragma: no cover - never called
                ...

        with pytest.raises(TypeError, match="read_drawing"):
            Forgetful()

    def test_a_drawing_document_reports_itself_as_one(self, session):
        info = session.backend.new_drawing("D", sheet="a3")
        assert info.kind == "drawing"

    def test_a_view_of_a_drawing_is_refused(self, session):
        from inventor_mcp.backend.base import ViewRequest

        drawing = session.backend.new_drawing("D")
        with pytest.raises(Exception, match="nothing to draw"):
            session.backend.place_view(drawing.id, ViewRequest(
                part_doc_id=drawing.id, name="FRONT"))

    def test_two_views_cannot_share_a_name_on_one_sheet(self, session):
        from inventor_mcp.backend.base import ViewRequest
        from inventor_mcp.builder import build_part

        built = build_part(session, PART)
        drawing = session.backend.new_drawing("D")
        request = ViewRequest(part_doc_id=built["document"], name="FRONT")
        session.backend.place_view(drawing.id, request)
        with pytest.raises(Exception, match="already has a view named"):
            session.backend.place_view(drawing.id, request)


class TestWhatARetrievedDimensionActuallyStates:
    """The subtlety the shipped example found, and the reason to read the sheet.

    Asking to dimension a parameter does not guarantee the sheet shows that
    parameter's value. Retrieval can only place a dimension the model *holds*,
    so a parameter the model never states on its own comes back as the dimension
    it drives. The mounting plate is the case: an `edge_margin` of 12 exists
    only as a hole spacing of `plate_w - 2 * edge_margin`, so a sheet asking for
    the margin carries 96 mm and 12 appears nowhere.

    The rehearsal cannot see this -- it resolves the parameter to 12, because
    that is what the parameter is worth -- and only a sheet that has been made
    can say which dimension came back. That is the difference the round trip
    exists for.
    """

    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def built(self, session):
        from inventor_mcp.drafting import build_drawing

        drawing = DrawingRecipe.model_validate(json.loads(
            (self.ROOT / "examples" / "drawings" / "mounting_plate.json").read_text(
                encoding="utf-8")))
        part = PartRecipe.model_validate(json.loads(
            (self.ROOT / "examples" / "mounting_plate.json").read_text(encoding="utf-8")))
        return build_drawing(session, drawing, part)

    def stated(self, session):
        return {entry["parameter"]: (entry["value"], entry.get("expression"))
                for entry in self.built(session)["read_back"]["dimensions"]}

    def test_the_margin_comes_back_as_the_spacing_it_drives(self, session):
        value, expression = self.stated(session)["edge_margin"]
        assert value == pytest.approx(9.6)  # 96 mm, in cm
        assert expression == "plate_w - 2 * edge_margin"

    def test_and_that_is_said_rather_than_left_to_be_noticed(self, session):
        assert any("rather than the parameter" in warning["warning"]
                   for warning in self.built(session)["warnings"])

    KNOWN = {"plate_w", "plate_d", "thk", "hole_d", "corner_r", "edge_margin"}

    def test_a_number_on_the_sheet_is_not_warned_about(self, session):
        """Measured on Inventor 2027.1, 2026-09-09: a retrieved dimension
        answers neither `ModelDimension.Parameter.Expression` nor
        `Parameter.Expression`, so its expression falls back to the text on the
        sheet -- `'120,00'` for a 120 mm plate, in the seat's own decimal
        separator. That is never the parameter's name, so warning on
        "expression != parameter" alone warned about every dimension on every
        live sheet, which is the fastest way to make a warning ignored.

        **And a number is not always bare**, which took a second run to find:
        `'R10,00'` for a radius parses as the identifier `R10` and `'n6,60'`
        for a diameter as `n6`, so sheet decoration read as a formula and the
        warning came back for every dimension carrying a prefix. Only names the
        part actually has count."""
        from inventor_mcp.drafting import _mentions_a_parameter_other_than as formula

        assert formula("plate_w - 2 * edge_margin", "edge_margin", self.KNOWN) is True
        assert formula("plate_w", "plate_w", self.KNOWN) is False
        assert formula("120,00", "plate_w", self.KNOWN) is False, \
            "a number is not a formula"
        assert formula("6,60 mm", "hole_d", self.KNOWN) is False
        assert formula("R10,00", "corner_r", self.KNOWN) is False, \
            "the R is Inventor's radius prefix, not a parameter called R10"
        assert formula("n6,60", "hole_d", self.KNOWN) is False, \
            "the diameter glyph is not a parameter called n6"
        assert formula("", "plate_w", self.KNOWN) is False
        # A different parameter entirely is still worth saying.
        assert formula("plate_d", "plate_w", self.KNOWN) is True
        # And a name nothing in the part has is decoration, whatever it parses as.
        assert formula("R10", "corner_r", set()) is False

    def test_a_parameter_the_model_states_directly_comes_back_as_itself(self, session):
        """Otherwise the warning above would be on everything and mean nothing."""
        stated = self.stated(session)
        for name in ("plate_w", "plate_d", "thk", "hole_d", "corner_r"):
            assert stated[name][1] == name, name

    def test_an_exact_match_is_preferred_over_one_that_merely_refers(self, session):
        """`plate_w` is referenced by the outline's width *and* by the hole
        spacing, so without a preference the answer would be whichever the
        iteration reached first -- and a drawing asking for the plate's width
        would sometimes get its hole pitch."""
        value, expression = self.stated(session)["plate_w"]
        assert expression == "plate_w"
        assert value == pytest.approx(12.0)

    def test_the_rehearsal_and_the_sheet_disagree_about_it_on_purpose(self, session):
        """The one place the two directions give different answers, and the
        reason `build_drawing` reads the sheet instead of trusting the ledger."""
        rehearsed = rehearse_drawing(
            DrawingRecipe.model_validate(json.loads(
                (self.ROOT / "examples" / "drawings" / "mounting_plate.json").read_text(
                    encoding="utf-8"))),
            PartRecipe.model_validate(json.loads(
                (self.ROOT / "examples" / "mounting_plate.json").read_text(
                    encoding="utf-8"))))
        ledger = {entry["label"]: entry["value"]
                  for view in rehearsed["ledger"]["views"]
                  for entry in view["dimensions"]}
        assert ledger["edge_margin"] == pytest.approx(12.0)
        assert self.stated(session)["edge_margin"][0] == pytest.approx(9.6)


class TestProjectedViews:
    """Where a projected view lands is what first and third angle *mean*.

    Before this, `DrawingRecipe.projection` was recorded and applied to nothing:
    every view was a base view at a position the recipe gave, so the sheet
    stated a convention it did not follow. A reader trusts the projection
    symbol, so that was a promise the schema made and did not keep.
    """

    def sheet(self, projection, views):
        return {"name": "D", "template": "t.idw", "projection": projection,
                "views": views}

    THREE = [
        {"name": "FRONT", "direction": "front", "at": [100, 100],
         "dimension": ["plate_w", "plate_t"]},
        {"name": "TOP", "direction": "top", "parent": "FRONT",
         "dimension": ["plate_d"]},
        {"name": "RIGHT", "direction": "right", "parent": "FRONT"},
    ]

    def built(self, session, projection):
        from inventor_mcp.drafting import build_drawing

        report = build_drawing(
            session, DrawingRecipe.model_validate(self.sheet(projection, self.THREE)),
            PART)
        return {entry["view"]["name"]: tuple(entry["view"]["at"])
                for entry in report["views"]}

    def test_third_angle_draws_the_top_view_above_the_front(self, session):
        at = self.built(session, "third_angle")
        assert at["TOP"][1] > at["FRONT"][1]
        assert at["RIGHT"][0] > at["FRONT"][0]

    def test_first_angle_draws_it_below_and_the_right_view_left(self, session):
        """The whole of the difference between the two conventions, and the
        reason a sheet has to say which it uses: reading one as the other
        mirrors the part, and the part is not what is wrong."""
        at = self.built(session, "first_angle")
        assert at["TOP"][1] < at["FRONT"][1]
        assert at["RIGHT"][0] < at["FRONT"][0]

    def test_the_two_conventions_are_mirror_images_about_the_parent(self):
        from inventor_mcp.drafting import projected_position

        for direction in ("top", "bottom", "left", "right"):
            third = projected_position(direction, "third_angle", (100.0, 100.0), 60.0)
            first = projected_position(direction, "first_angle", (100.0, 100.0), 60.0)
            assert (third[0] + first[0], third[1] + first[1]) == (200.0, 200.0), direction

    def test_an_isometric_goes_in_the_same_corner_either_way(self):
        """It is not a projection of anything, so neither convention has an
        opinion about where it goes -- and flipping it would move it for no
        reason."""
        from inventor_mcp.drafting import projected_position

        assert (projected_position("iso", "third_angle", (0.0, 0.0), 60.0)
                == projected_position("iso", "first_angle", (0.0, 0.0), 60.0))

    def test_a_projected_view_takes_its_parents_scale(self, session):
        from inventor_mcp.drafting import build_drawing

        report = build_drawing(session, DrawingRecipe.model_validate(self.sheet(
            "third_angle", [
                {"name": "FRONT", "direction": "front", "at": [100, 100],
                 "scale": 0.5, "dimension": ["plate_w", "plate_d", "plate_t", "hole_d"]},
                {"name": "TOP", "direction": "top", "parent": "FRONT", "scale": 2.0},
            ])), PART)
        scales = {entry["view"]["name"]: entry["view"]["scale"]
                  for entry in report["views"]}
        assert scales == {"FRONT": pytest.approx(0.5), "TOP": pytest.approx(0.5)}

    def test_the_gap_is_an_expression_like_every_other_length(self, session):
        from inventor_mcp.drafting import build_drawing

        report = build_drawing(session, DrawingRecipe.model_validate(self.sheet(
            "third_angle", [
                {"name": "FRONT", "direction": "front", "at": [100, 100],
                 "dimension": ["plate_w", "plate_d", "plate_t", "hole_d"]},
                {"name": "TOP", "direction": "top", "parent": "FRONT",
                 "gap": "plate_w / 2"},
            ])), PART)
        at = {entry["view"]["name"]: entry["view"]["at"] for entry in report["views"]}
        # 60 mm above the front view: half of the plate's 120 mm width.
        assert at["TOP"][1] == pytest.approx(16.0)

    def test_parents_are_placed_before_the_views_projected_from_them(self, session):
        """Recipe order need not be dependency order, and a projected view's
        position comes from where its parent actually went."""
        from inventor_mcp.drafting import build_drawing

        report = build_drawing(session, DrawingRecipe.model_validate(self.sheet(
            "third_angle", [
                {"name": "TOP", "direction": "top", "parent": "FRONT",
                 "dimension": ["plate_d"]},
                {"name": "FRONT", "direction": "front", "at": [100, 100],
                 "dimension": ["plate_w", "plate_t", "hole_d"]},
            ])), PART)
        assert report["ok"] is True
        at = {entry["view"]["name"]: entry["view"]["at"] for entry in report["views"]}
        assert at["TOP"] == pytest.approx([10.0, 16.0])

    def test_a_parent_that_does_not_exist_is_refused_by_name(self, session):
        from inventor_mcp.drafting import build_drawing

        report = build_drawing(session, DrawingRecipe.model_validate(self.sheet(
            "third_angle", [
                {"name": "FRONT", "direction": "front", "at": [100, 100],
                 "dimension": ["plate_w", "plate_d", "plate_t", "hole_d"]},
                {"name": "TOP", "direction": "top", "parent": "NOSUCH"},
            ])), PART)
        assert report["ok"] is False
        assert "no view named 'NOSUCH'" in report["findings"][0]["error"]

    def test_the_layout_is_readable_from_a_rehearsal_without_a_sheet(self):
        """So the projection angle's effect can be checked before anything is made."""
        third = rehearse_drawing(
            DrawingRecipe.model_validate(self.sheet("third_angle", self.THREE)), PART)
        first = rehearse_drawing(
            DrawingRecipe.model_validate(self.sheet("first_angle", self.THREE)), PART)

        def top_of(report):
            return next(view["at"] for view in report["ledger"]["views"]
                        if view["name"] == "TOP")

        assert top_of(third) == [100.0, 160.0]
        assert top_of(first) == [100.0, 40.0]

    def test_the_shipped_drawing_is_first_angle_and_draws_the_top_view_below(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        drawing = DrawingRecipe.model_validate(json.loads(
            (root / "examples" / "drawings" / "mounting_plate.json").read_text(
                encoding="utf-8")))
        part = PartRecipe.model_validate(json.loads(
            (root / "examples" / "mounting_plate.json").read_text(encoding="utf-8")))
        assert drawing.projection == "first_angle"
        at = {view["name"]: view["at"]
              for view in rehearse_drawing(drawing, part)["ledger"]["views"]}
        assert at["TOP"][1] < at["FRONT"][1], "first angle puts the top view below"


class TestExportingTheSheet:
    """A drawing that cannot be sent is not finished.

    Only the format table is testable here: the simulator writes no files and
    says so, and whether Inventor's PDF translator is enabled on a given machine
    is exactly the sort of thing `export`'s written-but-not-there check exists to
    report. What matters is that `pdf` is offered at all -- a drawing exportable
    only as DWG is one the factory needs Inventor to read.
    """

    def test_pdf_is_an_export_format(self):
        from inventor_mcp.backend.com.backend import EXPORT_EXTENSIONS

        assert EXPORT_EXTENSIONS["pdf"] == ".pdf"

    def test_the_tool_offers_it(self, server):
        import asyncio

        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        assert "pdf" in str(tools["export_model"].input_schema)

    def test_the_simulator_says_it_wrote_nothing(self, session):
        from inventor_mcp.backend.base import ExportRequest

        drawing = session.backend.new_drawing("D")
        result = session.backend.export(
            drawing.id, ExportRequest(path="/tmp/nope.pdf", format="pdf"))
        assert result["written"] is False and result["simulated"] is True


class TestWhatPointingItAtEveryShippedPartFound:
    """Five defects, found by asking for a drawing of all eleven shipped parts.

    None of them by writing a test first. The fixture at the top of this file is
    a plate with four well-behaved parameters, and every one of these needed a
    part shaped some other way: a pulley with a count, a cover plate with
    counterbored holes, a housing with a draft angle, a pipe bend whose wall is
    an intermediate parameter. `ARCHITECTURE.md` already argues for keeping the
    examples executable; this is that argument paying out again.

    The sweep at the end is the harness itself, kept as a test so the next
    schema change is checked against every shipped part rather than against the
    plate.
    """

    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def part(self, stem):
        return PartRecipe.model_validate(json.loads(
            (self.ROOT / "examples" / f"{stem}.json").read_text(encoding="utf-8")))

    def drawn(self, session, stem, dimension):
        from inventor_mcp.drafting import build_drawing

        return build_drawing(session, DrawingRecipe.model_validate({
            "name": "D", "template": "t.idw", "views": [
                {"name": "FRONT", "dimension": dimension}]}), self.part(stem))

    def test_an_angle_is_stated_in_degrees_and_not_in_radians(self, session):
        """The moulded housing's 1.5 degree draft came out as 0.02618 -- its
        value in radians -- because an angle was divided by the *length* factor.
        `compare` then read 0.02618 as degrees and called it a number the part
        does not have, so the sheet was reported wrong and the reason was a unit.
        """
        report = rehearse_drawing(
            DrawingRecipe.model_validate({"name": "D", "template": "t.idw", "views": [
                {"name": "FRONT", "dimension": ["draft_a"]}]}),
            self.part("moulded_housing"))
        entry = report["ledger"]["views"][0]["dimensions"][0]
        assert entry["value"] == pytest.approx(1.5)
        assert entry["units"] == "deg"
        assert report["round_trip"]["states_what_the_part_does_not_have"] == []

    def test_an_angle_in_another_unit_still_reaches_the_reading_as_degrees(self):
        """`DrawingReading` has no angle unit: `compare` turns the model's
        radians into degrees and compares numbers, so a sheet stating gradians
        has to be converted rather than handed over."""
        from inventor_mcp.drafting import as_reading

        recipe = DrawingRecipe.model_validate({
            "name": "D", "template": "t.idw", "angle_units": "rad", "views": [
                {"name": "FRONT", "dimension": ["draft_a"]}]})
        report = rehearse_drawing(recipe, self.part("moulded_housing"))
        assert report["ledger"]["views"][0]["dimensions"][0]["value"] == pytest.approx(
            0.02618, abs=5e-6)
        reading = as_reading(recipe, report["ledger"])
        assert reading.dimensions[0].value == pytest.approx(1.5, abs=1e-4)

    def test_a_counterbores_own_sizes_can_be_dimensioned(self, session):
        """The hole feature recorded its diameter and style and not its
        counterbore's diameter or depth, so nothing could retrieve them -- and a
        counterbore's diameter is a dimension any drawing of this plate carries.
        """
        report = self.drawn(session, "cover_plate",
                            ["cbore_d", "cbore_deep", "csink_d", "csink_inc"])
        placed = {entry["parameter"]: entry["value"]
                  for entry in report["views"][0]["dimensions"]}
        assert set(placed) == {"cbore_d", "cbore_deep", "csink_d", "csink_inc"}
        assert report["ok"] is True

    def test_a_count_is_refused_rather_than_resolved_as_a_length(self, session):
        """The worst-behaved of the five. The belt pulley's `lighten_count` of 5
        was resolved as a length and reported as a 50 mm dimension -- a number
        that appears nowhere on the part or the sheet. It was only caught at all
        because the part happens to have no 50 mm number either.
        """
        report = self.drawn(session, "belt_pulley", ["lighten_count"])
        assert report["ok"] is False
        assert "cannot be a dimension" in report["findings"][0]["error"]
        assert "unitless" in report["findings"][0]["error"]

    def test_the_count_is_refused_before_any_sheet_is_made(self, session):
        """Building has to check what rehearsing checks. Otherwise a caller who
        skips the rehearsal gets the category error reported as a dimension that
        did not arrive -- which is what a typo looks like, and the two have
        different fixes."""
        report = self.drawn(session, "belt_pulley", ["lighten_count"])
        assert "document" not in report
        assert "read_back" not in report

    def test_the_rehearsal_refuses_it_too(self):
        report = rehearse_drawing(
            DrawingRecipe.model_validate({"name": "D", "template": "t.idw", "views": [
                {"name": "FRONT", "dimension": ["lighten_count"]}]}),
            self.part("belt_pulley"))
        assert report["ok"] is False
        assert "cannot be a dimension" in report["findings"][0]["error"]

    def test_an_expression_mixing_a_count_into_a_length_is_fine(self):
        """Only bare names are refused: `pitch * holes` is a length by the time
        it is resolved and is perfectly drawable."""
        report = rehearse_drawing(
            DrawingRecipe.model_validate({"name": "D", "template": "t.idw", "views": [
                {"name": "FRONT", "dimension": ["lighten_pcd / lighten_count"]}]}),
            self.part("belt_pulley"))
        assert report["ok"] is True or not report["findings"]

    def test_an_intermediate_parameter_is_explained_by_name(self, session):
        """`pipe_bend`'s wall appears only in `tube_od = tube_id + 2 * wall`. It
        drives the part and no dimension states it -- the sketch says `tube_od`
        -- so there is nothing to retrieve, and the useful answer is the name of
        the parameter that *is* stated."""
        report = self.drawn(session, "pipe_bend", ["wall"])
        why = " ".join(warning["why"] for warning in report["warnings"])
        assert "wall drives geometry only through tube_od" in why
        assert "dimension tube_od instead" in why

    def test_a_parameter_driving_nothing_at_all_is_said_differently(self, session):
        """The two have different fixes, so they get different sentences."""
        part = PartRecipe.model_validate({
            "name": "P", "units": "mm",
            "parameters": [{"name": "w", "value": 50},
                           {"name": "spare", "value": 3, "comment": "drives nothing"}],
            "operations": [
                {"op": "sketch", "name": "O", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": "w", "height": 30}]},
                {"op": "extrude", "name": "B", "sketch": "O", "distance": 5},
            ]})
        from inventor_mcp.drafting import build_drawing

        report = build_drawing(session, DrawingRecipe.model_validate({
            "name": "D", "template": "t.idw", "views": [
                {"name": "FRONT", "dimension": ["w", "spare"]}]}), part)
        why = " ".join(warning["why"] for warning in report["warnings"])
        assert "no sketch dimension and no feature value at all" in why

    #: Every shipped part, and what a drawing of it can and cannot state. Held
    #: as data so a change to the retrieval has to be agreed with here -- which
    #: is what would have caught three of the five above on the day they landed.
    #:
    #: A name in the second element is a parameter no dimension can state, and
    #: each is a *correct* answer with a reason: `lighten_count` is a count,
    #: `boss_wall` and `wall` drive geometry only through another parameter, and
    #: `thread_d`/`thread_pitch` feed a tap designation the same way.
    SHIPPED = [
        ("angle_bracket", []),
        ("belt_pulley", ["lighten_count"]),
        ("cover_plate", []),
        ("duct_transition", []),
        ("enclosure_base", []),
        ("flanged_shaft", []),
        ("hex_standoff", []),
        ("moulded_housing", ["boss_wall"]),
        ("mounting_plate", []),
        ("pipe_bend", ["wall"]),
        ("threaded_boss", ["thread_d", "thread_pitch"]),
    ]

    @pytest.mark.parametrize("stem,unstatable", SHIPPED, ids=[s for s, _ in SHIPPED])
    def test_a_drawing_of_every_shipped_part_states_what_it_can(
            self, session, stem, unstatable):
        part = self.part(stem)
        names = [spec.name for spec in part.parameters]
        report = self.drawn(session, stem, names)
        if any(name in unstatable for name in names) and report["ok"] is False:
            # Refused rather than drawn, which is the right answer for a count.
            assert report["findings"], stem
            return
        arrived = {entry["parameter"] for entry in report["read_back"]["dimensions"]
                   if entry.get("parameter")}
        assert sorted(set(names) - arrived) == sorted(
            name for name in unstatable if name in names), stem

    def test_the_list_above_names_every_shipped_part(self):
        """So a new example cannot quietly stop being drawn."""
        shipped = {path.stem for path in (self.ROOT / "examples").glob("*.json")}
        assert {stem for stem, _ in self.SHIPPED} == shipped
