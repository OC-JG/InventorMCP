"""The simulator's volume model: signed prisms, read in order, one per body.

Before this the model was a single number and an append-only list. Only joined
extrudes were recorded and nothing ever removed from the list, so a cut through
a shelled box was charged against the solid the box had been *before* it was
hollowed. The enclosure's cable entry -- a 12 x 6 mm slot through a box whose
walls are 2.5 mm -- was charged the full 70 mm of box: 5.04 cm^3 against the
0.36 it removes, a 14-fold over-count, and the finished part came out 10.7%
light. `docs/FEATURE_COVERAGE.md` had it recorded as defect 2 for months.

Two things were wrong and both are here:

* nothing subtracted, so a void was invisible;
* one scalar volume, so a cut that removed more than the body it was aimed at
  contained was quietly paid for out of another body.

The other half of the same story is the outline. A fillet on the edges running
along a prism changes that prism's profile, and the shell that follows measures
the profile: the enclosure's 6 mm corners left square made its cavity 10.5 mm^2
too big.
"""

from __future__ import annotations

import json
import math
import pathlib

import pytest

from inventor_mcp.backend.mock.backend import (
    _add_span,
    _material_spans,
    _remove_span,
    _span_length,
)
from inventor_mcp.builder import build_part, rehearse
from inventor_mcp.geometry import inset_polygon, treat_polygon_corner
from inventor_mcp.schema import PartRecipe
from inventor_mcp.session import Session

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def session() -> Session:
    session = Session(backend_kind="mock")
    session.ensure_backend().connect()
    return session


def build(session: Session, operations: list[dict], name: str = "L") -> dict:
    result = build_part(session, PartRecipe.model_validate(
        {"name": name, "units": "mm", "operations": operations}))
    assert result["ok"], result["errors"]
    return result


def volume(session: Session, result: dict) -> float:
    return session.backend.mass_properties(result["document"]).volume


#: A 100 x 70 x 35 box, hollowed to 2.5 mm walls with its top open. The shape
#: every enclosure in this repository is, and the shape the old model was worst
#: at.
SHELLED_BOX = [
    {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 100, "height": 70}]},
    {"op": "extrude", "name": "Block", "sketch": "Outline", "distance": 35},
    {"op": "shell", "name": "Cavity", "thickness": 2.5, "direction": "inside",
     "faces": {"kind": "face", "filter": "top"}},
]


class TestASpanIsMaterialInPieces:
    """The arithmetic the ledger is read with, on its own."""

    def test_filling_in_a_gap_merges_the_neighbours(self):
        assert _add_span([(0.0, 1.0), (2.0, 3.0)], (1.0, 2.0)) == [(0.0, 3.0)]

    def test_a_span_inside_another_changes_nothing(self):
        assert _add_span([(0.0, 3.0)], (1.0, 2.0)) == [(0.0, 3.0)]

    def test_taking_the_middle_out_leaves_two(self):
        assert _remove_span([(0.0, 3.0)], (1.0, 2.0)) == [(0.0, 1.0), (2.0, 3.0)]

    def test_taking_out_more_than_is_there_leaves_nothing(self):
        assert _remove_span([(1.0, 2.0)], (0.0, 9.0)) == []

    def test_length_adds_the_pieces_up(self):
        assert _span_length([(0.0, 1.0), (5.0, 7.0)]) == pytest.approx(3.0)

    def test_a_window_counts_only_what_is_inside_it(self):
        assert _span_length([(0.0, 1.0), (5.0, 7.0)], (0.5, 6.0)) == pytest.approx(1.5)


class TestAShellIsRecordedAsAVoid:
    def test_the_wall_is_measured_where_the_solid_used_to_be(self, session):
        """Two 2.5 mm walls, 65 mm of air, and not a 70 mm prism of box."""
        result = build(session, SHELLED_BOX)
        document = session.backend._doc(result["document"])
        # Along Y, through the middle of the box at two thirds of its height.
        spans = _material_spans(document, 1, (0.0, 0.0, 2.95))
        assert spans == [pytest.approx((-3.5, -3.25)), pytest.approx((3.25, 3.5))]
        assert _span_length(spans) == pytest.approx(0.5)

    def test_a_cut_through_it_is_charged_the_walls(self, session):
        result = build(session, SHELLED_BOX + [
            {"op": "sketch", "name": "Cable", "plane": "xz", "entities": [
                {"type": "rectangle", "center": [0, 29.5], "width": 12, "height": 6}]},
            {"op": "extrude", "name": "CableCut", "sketch": "Cable", "distance": 70,
             "direction": "symmetric", "operation": "cut"},
        ])
        cut = result["operations"][-1]
        # 12 x 6 mm through 2 x 2.5 mm of wall.
        assert cut["measured"]["volume_change_cm3"] == pytest.approx(-1.2 * 0.6 * 0.5)
        assert "material the cut meets" in cut["detail"]["volume_from"]

    def test_a_fallback_shell_records_nothing_rather_than_a_guess(self, session):
        """A revolved body is not a prism, so its cavity is not one either."""
        result = build(session, [
            {"op": "sketch", "name": "P", "plane": "xz", "entities": [
                {"type": "rectangle", "corner": [0, 0], "width": 20, "height": 30}]},
            {"op": "revolve", "name": "Blank", "sketch": "P", "axis": "z"},
            {"op": "shell", "thickness": 2, "faces": {"kind": "face", "filter": "top"}},
        ], name="Cup")
        document = session.backend._doc(result["document"])
        assert document.slabs == []


class TestACutIsRecordedToo:
    def test_the_same_cut_twice_removes_the_material_once(self, session):
        """It used to be charged in full both times, and said so in a comment."""
        plate = [
            {"op": "sketch", "name": "Body", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]},
            {"op": "extrude", "name": "Plate", "sketch": "Body", "distance": 8},
        ]
        slot = [
            {"op": "sketch", "name": "Slot", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 10, "height": 10}]},
            {"op": "extrude", "sketch": "Slot", "extent": "through_all",
             "operation": "cut"},
        ]
        result = build(session, plate + slot + [
            {"op": "sketch", "name": "Again", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 10, "height": 10}]},
            {"op": "extrude", "sketch": "Again", "distance": 8, "operation": "cut"},
        ])
        assert result["operations"][-1]["measured"]["volume_change_cm3"] == pytest.approx(0.0)
        assert volume(session, result) == pytest.approx(6 * 4 * 0.8 - 1.0 * 1.0 * 0.8)

    def test_material_put_back_into_a_void_counts_again(self, session):
        """Which is why the ledger is read in order rather than summed by sign."""
        result = build(session, SHELLED_BOX + [
            {"op": "sketch", "name": "Pillar", "plane": "xy", "offset": 2.5, "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 20, "height": 20}]},
            {"op": "extrude", "name": "Pillar", "sketch": "Pillar", "distance": 20},
        ])
        document = session.backend._doc(result["document"])
        # Up the middle: 2.5 mm of floor, then 20 mm of pillar standing in the
        # cavity, and no air between them.
        assert _material_spans(document, 2, (0.0, 0.0, 0.0)) == [pytest.approx((0.0, 2.25))]


class TestOneBodyDoesNotPayForAnother:
    """The reason a volume is kept per body and only ever added up to report.

    A cut that removes more than the body it was aimed at holds is a mistake
    worth seeing. With one scalar for the part it is not visible at all: the
    surplus comes off some other body's material and the total stays plausible.
    """

    TWO = [
        {"op": "sketch", "name": "A", "plane": "xy", "entities": [
            {"type": "rectangle", "corner": [0, 0], "width": 20, "height": 20}]},
        {"op": "extrude", "name": "BodyA", "sketch": "A", "distance": 10},
        {"op": "sketch", "name": "B", "plane": "xy", "entities": [
            {"type": "rectangle", "corner": [40, 0], "width": 20, "height": 20}]},
        {"op": "extrude", "name": "BodyB", "sketch": "B", "distance": 10,
         "operation": "new_body"},
    ]

    def test_an_over_cut_body_stops_at_nothing_and_leaves_the_other_alone(self, session):
        result = build(session, self.TWO + [
            {"op": "sketch", "name": "Huge", "plane": "xy", "entities": [
                {"type": "rectangle", "corner": [0, 0], "width": 20, "height": 20}]},
            {"op": "extrude", "sketch": "Huge", "distance": 500, "operation": "cut",
             "bodies": [1]},
        ], name="Pair")
        document = session.backend._doc(result["document"])
        assert document.bodies[0] == pytest.approx(0.0)
        assert document.bodies[1] == pytest.approx(4.0), "body B was not touched"

    def test_what_it_reports_moving_is_what_moved(self, session):
        result = build(session, self.TWO + [
            {"op": "sketch", "name": "Huge", "plane": "xy", "entities": [
                {"type": "rectangle", "corner": [0, 0], "width": 20, "height": 20}]},
            {"op": "extrude", "sketch": "Huge", "distance": 500, "operation": "cut",
             "bodies": [1]},
        ], name="Pair")
        assert result["operations"][-1]["measured"]["volume_change_cm3"] == pytest.approx(-4.0)

    def test_the_bodies_are_kept_current_by_every_feature(self, session):
        """Not only by extrude, which is the one that used to update them."""
        result = build(session, SHELLED_BOX)
        document = session.backend._doc(result["document"])
        assert document.bodies == [pytest.approx(document.volume)]

    def test_the_volume_is_derived_and_cannot_be_assigned(self, session):
        result = build(session, SHELLED_BOX)
        document = session.backend._doc(result["document"])
        with pytest.raises(AttributeError):
            document.volume = 1.0

    def test_a_cut_aimed_at_a_body_is_measured_against_that_body(self, session):
        """Body A's profile is over thin air where body B stands."""
        result = build(session, self.TWO + [
            {"op": "sketch", "name": "Bore", "plane": "xy", "entities": [
                {"type": "circle", "center": [50, 10], "diameter": 8}]},
            {"op": "extrude", "sketch": "Bore", "extent": "through_all",
             "operation": "cut", "bodies": [2]},
        ], name="Pair")
        document = session.backend._doc(result["document"])
        assert document.bodies[0] == pytest.approx(4.0), "body A is untouched"
        assert document.bodies[1] == pytest.approx(4.0 - math.pi * 0.4**2 * 1.0)


class TestAFilletMovesTheOutline:
    ROUNDED = [
        {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
            {"type": "rectangle", "center": [0, 0], "width": 100, "height": 70}]},
        {"op": "extrude", "name": "Block", "sketch": "Outline", "distance": 35},
        {"op": "fillet", "name": "Corners", "edges": {"filter": "vertical"}, "radius": 6},
    ]

    def test_it_says_how_many_corners_it_moved(self, session):
        result = build(session, self.ROUNDED)
        assert result["operations"][-1]["detail"]["outline_corners"] == 4

    def test_the_shell_that_follows_measures_the_rounded_profile(self, session):
        """95 x 65 with r3.5 corners, not 95 x 65 square: 10.5 mm^2 of difference."""
        result = build(session, self.ROUNDED + [
            {"op": "shell", "name": "Cavity", "thickness": 2.5, "direction": "inside",
             "faces": {"kind": "face", "filter": "top"}}])
        cavity = (9.5 * 6.5 - (4 - math.pi) * 0.35**2) * 3.25
        assert result["operations"][-1]["measured"]["volume_change_cm3"] == pytest.approx(
            -cavity, rel=2e-5)

    def test_an_edge_that_is_not_a_corner_of_a_prism_leaves_it_alone(self, session):
        """Rounding the rim of a block does not move the block's profile."""
        result = build(session, self.ROUNDED[:-1] + [
            {"op": "fillet", "edges": {"filter": "horizontal"}, "radius": 1}])
        assert result["operations"][-1]["detail"]["outline_corners"] == 0

    def test_a_variable_fillet_is_not_one_arc_so_it_does_not_pretend(self, session):
        result = build(session, self.ROUNDED[:-1] + [
            {"op": "fillet", "edges": {"filter": "vertical"}, "radius": 3,
             "radius_end": 8}])
        assert result["operations"][-1]["detail"]["outline_corners"] == 0


class TestTheCornerItself:
    SQUARE = [(-5.0, -3.5), (5.0, -3.5), (5.0, 3.5), (-5.0, 3.5)]

    def test_a_radius_that_would_eat_its_neighbours_is_refused(self):
        assert treat_polygon_corner(self.SQUARE, 0, radius=9.0) is None

    def test_a_chamfer_is_one_chord(self):
        cut = treat_polygon_corner(self.SQUARE, 0, setbacks=(0.3, 0.3))
        assert cut[:2] == [(-5.0, -3.2), (-4.7, -3.5)]

    def test_a_straight_run_has_no_corner_to_treat(self):
        collinear = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (1.0, 2.0)]
        assert treat_polygon_corner(collinear, 1, radius=0.1) is None

    def test_an_offset_that_folds_over_itself_is_refused(self):
        assert inset_polygon(self.SQUARE, 5.0) is None

    def test_and_one_that_does_not_is_exact(self):
        assert inset_polygon(self.SQUARE, 0.25) == [
            (-4.75, -3.25), (4.75, -3.25), (4.75, 3.25), (-4.75, 3.25)]


class TestTheEnclosureItself:
    """The part the whole defect was measured on, against a hand calculation."""

    def recipe(self) -> PartRecipe:
        return PartRecipe.model_validate(
            json.loads((ROOT / "examples/enclosure_base.json").read_text()))

    def expected(self) -> dict:
        return json.loads((ROOT / "examples/expected/enclosure_base.json").read_text())

    def test_it_now_agrees_with_the_arithmetic_that_was_done_by_hand(self, session):
        """46.896177 cm^3, derived term by term before any of this was written.

        The simulator was 10.7% below it. What is left is the arc sampling in a
        rounded corner -- the outline carries a polygon where the part has an
        arc -- and is under three thousandths of a percent.
        """
        result = build_part(session, self.recipe())
        assert result["ok"], result["errors"]
        assert volume(session, result) == pytest.approx(
            self.expected()["volume_cm3"], rel=5e-5)

    def test_the_cable_entry_removes_two_walls(self, session):
        result = build_part(session, self.recipe())
        cut = [op for op in result["operations"] if op.get("name") == "CableCut"][0]
        assert cut["measured"]["volume_change_cm3"] == pytest.approx(-0.36)

    def test_the_rehearsal_still_declines_to_predict_the_holes(self, session):
        """They are charged their full depth, which a hollow part can make wrong."""
        report = rehearse(self.recipe())
        steps = {step["op"]: step for step in report["steps"]}
        assert steps["hole"]["predictable"] is False

    def test_but_the_cut_after_the_shell_is_predicted_now(self, session):
        """It is measured against the ledger, so there is nothing to excuse."""
        report = rehearse(self.recipe())
        cut = [step for step in report["steps"] if step["name"] == "CableCut"][0]
        assert "predictable" not in cut
